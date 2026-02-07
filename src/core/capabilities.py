import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4


def probe_embedding() -> dict[str, Any]:
    url = (settings.EMBEDDING_URL or "").rstrip("/")
    out: dict[str, Any] = {"url": url or None, "max_tokens": None, "max_chars": None, "source": None}
    if not url:
        return out

    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{url}/info")
            r.raise_for_status()
            info = r.json()
            max_tokens = info.get("max_input_length")
            if max_tokens is not None:
                out["max_tokens"] = int(max_tokens)
                out["max_chars"] = int(max_tokens * CHARS_PER_TOKEN)
                out["source"] = "TEI /info"
                out["model_id"] = info.get("model_id")
                return out
    except Exception as e:
        logger.debug("Embedding /info failed: %s", e)
    try:
        with httpx.Client(timeout=30.0) as client:
            for n_chars in [500, 1000, 2000, 4000, 8000, 16000]:
                text = "x " * (n_chars // 2)
                r = client.post(f"{url}/embed", json={"inputs": text})
                if r.status_code != 200:
                    break
                out["max_chars"] = n_chars
                out["source"] = "probe"
            if out["max_chars"] is not None:
                out["max_tokens"] = out["max_chars"] // CHARS_PER_TOKEN
    except Exception as e:
        logger.debug("Embedding probe failed: %s", e)

    return out


def probe_vllm() -> dict[str, Any]:
    base = (settings.VLLM_URL or "").rstrip("/")
    out: dict[str, Any] = {"url": base or None, "max_model_len": None, "source": None}
    if not base:
        return out

    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{base}/models")
            r.raise_for_status()
            data = r.json()
            models = data.get("data") or []
            server_max: Optional[int] = None
            model_id: Optional[str] = None
            for m in models:
                if not isinstance(m, dict):
                    continue
                if model_id is None:
                    model_id = m.get("id") or settings.VLLM_MODEL
                for key in ("max_model_len", "context_length"):
                    val = m.get(key)
                    if val is not None:
                        try:
                            n = int(val)
                            if server_max is None or n > server_max:
                                server_max = n
                        except (TypeError, ValueError):
                            pass
            if data.get("max_model_len") is not None:
                try:
                    n = int(data["max_model_len"])
                    if server_max is None or n > server_max:
                        server_max = n
                except (TypeError, ValueError):
                    pass
            if server_max is not None:
                out["max_model_len"] = server_max
                out["source"] = "v1/models"
            if model_id is not None:
                out["model_id"] = model_id
    except Exception as e:
        logger.debug("vLLM /models failed: %s", e)

    return out


def probe_spacy_api() -> dict[str, Any]:
    url = (settings.SPACY_API_URL or "").rstrip("/")
    out: dict[str, Any] = {"url": url or None, "available": False}
    if not url:
        return out
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(f"{url}/ner", json={"text": "Hello world.", "lang": "en"})
            if r.status_code == 200:
                out["available"] = True
                return out
            if r.status_code == 422:
                try:
                    detail = r.json().get("detail", r.text)
                    out["422_detail"] = str(detail)[:200]
                except Exception:
                    out["422_detail"] = "Unprocessable Entity"
    except Exception as e:
        out["error"] = str(e)
    return out


def probe_fasttext_langdetect() -> dict[str, Any]:
    url = (settings.FASTTEXT_LANGDETECT_URL or "").rstrip("/")
    out: dict[str, Any] = {"url": url or None, "available": False}
    if not url:
        return out
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{url}/health")
            if r.status_code == 200:
                info = r.json() if r.content else {}
                if info.get("model_loaded") is True:
                    out["available"] = True
                    return out
    except Exception as e:
        out["error"] = str(e)
    return out


def run_all() -> dict[str, Any]:
    embedding = probe_embedding()
    vllm = probe_vllm()
    spacy_api_info = probe_spacy_api()
    fasttext_info = probe_fasttext_langdetect()

    result: dict[str, Any] = {
        "embedding": {
            "max_tokens": embedding.get("max_tokens"),
            "max_chars": embedding.get("max_chars"),
            "source": embedding.get("source"),
            "model_id": embedding.get("model_id"),
        },
        "vllm": {
            "max_model_len": vllm.get("max_model_len"),
            "source": vllm.get("source"),
            "model_id": vllm.get("model_id"),
        },
        "spacy_api": spacy_api_info,
        "fasttext_langdetect": fasttext_info,
    }

    if embedding.get("max_tokens") is not None:
        result["embedding"]["max_tokens"] = embedding["max_tokens"]
    if embedding.get("max_chars") is not None:
        result["embedding"]["max_chars"] = embedding["max_chars"]
    if vllm.get("max_model_len") is not None:
        max_ctx = vllm["max_model_len"]
        result["classify_body_max_chars"] = min(8000, (max_ctx * 4) // 2)
    if vllm.get("model_id") is not None:
        result["vllm"]["model_id"] = vllm["model_id"]

    return result


def write_capabilities_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Wrote %s", path)
