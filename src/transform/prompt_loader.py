import os
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_PROMPTS_DIR = _PROJECT_ROOT / "prompts"

_cached: dict | None = None


def get_prompts_dir() -> Path:
    env = os.environ.get("LILITH_PROMPTS_DIR", "").strip()
    if env:
        return Path(env).resolve()
    return _DEFAULT_PROMPTS_DIR


def _load_classification_prompts() -> dict:
    global _cached
    if _cached is not None:
        return _cached
    base = get_prompts_dir()
    system_path = base / "classification_system.md"
    user_path = base / "classification_user.md"
    if not system_path.exists():
        raise FileNotFoundError(
            "Classification system prompt not found: %s. Create it or set LILITH_PROMPTS_DIR."
            % system_path
        )
    if not user_path.exists():
        raise FileNotFoundError(
            "Classification user template not found: %s. Create it or set LILITH_PROMPTS_DIR."
            % user_path
        )
    system = system_path.read_text(encoding="utf-8").strip()
    user_template = user_path.read_text(encoding="utf-8")
    if user_template and not user_template.endswith("\n"):
        user_template = user_template + "\n"
    _cached = {"system": system, "user_template": user_template}
    return _cached


def get_classification_prompts() -> dict:
    return _load_classification_prompts()


def format_prompt(template: str, **kwargs: Any) -> str:
    return template.format(**kwargs)


def clear_prompt_cache() -> None:
    global _cached
    _cached = None
