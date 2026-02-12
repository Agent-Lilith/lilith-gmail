import json
from pathlib import Path

_DEFAULT_EMBED_MAX_TOKENS = 8192
_DEFAULT_EMBED_MAX_CHARS = 32768  # ~4 chars/token * 8192
_DEFAULT_CLASSIFY_MAX_CHARS = 6000

_capabilities_path: Path | None = None
_cached: dict | None = None


def _get_path() -> Path:
    if _capabilities_path is not None:
        return _capabilities_path
    return Path(__file__).resolve().parent.parent.parent / "capabilities.json"


def get_capabilities_path() -> Path:
    return _get_path()


def set_capabilities_path(path: Path | None) -> None:
    global _capabilities_path, _cached
    _capabilities_path = path
    _cached = None


def _load() -> dict:
    global _cached
    if _cached is not None:
        return _cached
    path = _get_path()
    if not path.exists():
        _cached = {}
        return _cached
    try:
        with open(path) as f:
            _cached = json.load(f)
        return _cached
    except Exception:
        _cached = {}
        return _cached


def require_capabilities_for_transform() -> dict:
    path = _get_path()
    if not path.exists():
        raise RuntimeError(
            "capabilities.json is missing. Run: uv run python main.py capabilities"
        )
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        raise RuntimeError("Could not load capabilities.json: %s" % e) from e
    embed = data.get("embedding") or {}
    vllm = data.get("vllm") or {}
    spacy_api = data.get("spacy_api") or {}
    fasttext = data.get("fasttext_langdetect") or {}
    missing = []
    if embed.get("max_tokens") is None:
        missing.append("embedding.max_tokens")
    if vllm.get("model_id") is None or not str(vllm.get("model_id", "")).strip():
        missing.append("vllm.model_id")
    if vllm.get("max_model_len") is None:
        missing.append("vllm.max_model_len")
    if not spacy_api.get("available"):
        missing.append("spacy_api.available")
    if not fasttext.get("available"):
        missing.append("fasttext_langdetect.available")
    if missing:
        raise RuntimeError(
            "capabilities.json is incomplete (missing: %s). Run: uv run python main.py capabilities"
            % ", ".join(missing)
        )
    global _cached
    _cached = data
    return data


def get_embed_max_tokens() -> int:
    data = _load()
    embed = data.get("embedding") or {}
    v = embed.get("max_tokens")
    if v is not None and isinstance(v, (int, float)):
        return int(v)
    return _DEFAULT_EMBED_MAX_TOKENS


def get_embed_max_chars() -> int:
    data = _load()
    embed = data.get("embedding") or {}
    v = embed.get("max_chars")
    if v is not None and isinstance(v, (int, float)):
        return int(v)
    return _DEFAULT_EMBED_MAX_CHARS


def get_classify_max_chars() -> int:
    data = _load()
    v = data.get("classify_body_max_chars")
    if v is not None and isinstance(v, (int, float)):
        return int(v)
    vllm = data.get("vllm") or {}
    max_tokens = vllm.get("max_model_len")
    if max_tokens is not None and isinstance(max_tokens, (int, float)):
        return min(_DEFAULT_CLASSIFY_MAX_CHARS, (int(max_tokens) * 4) // 2)
    raise RuntimeError(
        "capabilities.json missing vllm.max_model_len/classify_body_max_chars. "
        "Run: uv run python main.py capabilities"
    )


def get_classify_max_model_len() -> int:
    data = _load()
    vllm = data.get("vllm") or {}
    max_tokens = vllm.get("max_model_len")
    if max_tokens is not None and isinstance(max_tokens, (int, float)):
        return int(max_tokens)
    raise RuntimeError(
        "capabilities.json missing vllm.max_model_len. Run: uv run python main.py capabilities"
    )


def get_classify_total_context_chars() -> int:
    data = _load()
    vllm = data.get("vllm") or {}
    max_tokens = vllm.get("max_model_len")
    if max_tokens is not None and isinstance(max_tokens, (int, float)):
        return int((int(max_tokens) * 4) * 0.9)  # 90% of context in chars
    raise RuntimeError(
        "capabilities.json missing vllm.max_model_len. Run: uv run python main.py capabilities"
    )


def get_vllm_model_id() -> str:
    data = _load()
    vllm = data.get("vllm") or {}
    model_id = vllm.get("model_id")
    if model_id is not None and isinstance(model_id, str) and model_id.strip():
        return model_id.strip()
    from core.config import settings

    return settings.VLLM_MODEL
