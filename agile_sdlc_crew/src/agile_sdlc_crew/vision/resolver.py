"""Vision config resolver — yaml + env override.

Config dosyasi: config/vision_config.yaml. Dashboard tarafindan yazilir.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger("pipeline")

_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "vision_config.yaml"

# Bilinen vision modelleri (UI hint icin — exhaustive degil)
KNOWN_VISION_MODELS = {
    # Ollama
    "qwen2.5vl:7b": "ollama",
    "qwen2.5vl:32b": "ollama",
    "llava:7b": "ollama",
    "llava:13b": "ollama",
    "llama3.2-vision:11b": "ollama",
    # OpenAI / LiteLLM proxy
    "vertex_ai/claude-sonnet-4-6": "openai",
    "vertex_ai/claude-opus-4-6": "openai",
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "claude-sonnet-4-20250514": "openai",
}


@lru_cache(maxsize=1)
def load_config() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"  vision_config.yaml okuma hatasi: {e}")
        return {}


def reset_cache() -> None:
    load_config.cache_clear()


def get_provider() -> str:
    cfg = load_config()
    if cfg.get("provider"):
        return cfg["provider"]
    if os.environ.get("CREW_VISION_PROVIDER"):
        return os.environ["CREW_VISION_PROVIDER"]
    # Geriye uyumluluk: CREW_USE_LOCAL_VISION env'i
    if os.environ.get("CREW_USE_LOCAL_VISION", "").lower() in ("1", "true", "yes"):
        return "ollama"
    return "openai"


def get_model() -> str:
    cfg = load_config()
    if cfg.get("model"):
        return cfg["model"]
    provider = get_provider()
    if provider == "ollama":
        return os.environ.get("CREW_LOCAL_VISION_MODEL", "qwen2.5vl:7b")
    return os.environ.get("CREW_VISION_MODEL") or os.environ.get("LITELLM_MODEL", "vertex_ai/claude-sonnet-4-6")


def get_base_url() -> str:
    cfg = load_config()
    if cfg.get("base_url"):
        return cfg["base_url"]
    if os.environ.get("CREW_VISION_BASE_URL"):
        return os.environ["CREW_VISION_BASE_URL"]
    provider = get_provider()
    if provider == "ollama":
        return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    return (
        os.environ.get("LITELLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )


def get_api_key() -> str:
    cfg = load_config()
    if cfg.get("api_key_env"):
        return os.environ.get(cfg["api_key_env"], "")
    return (
        os.environ.get("LITELLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )


def save_config(
    provider: str,
    model: str,
    base_url: str = "",
    api_key_env: str = "",
) -> dict:
    """Dashboard'dan gelen ayari kaydet."""
    from agile_sdlc_crew.vision.registry import list_providers
    if provider not in list_providers():
        raise ValueError(
            f"Bilinmeyen vision provider: {provider}. Mevcut: {list_providers()}"
        )

    cfg: dict = {"provider": str(provider), "model": str(model)}
    if base_url:
        cfg["base_url"] = str(base_url).strip()
    if api_key_env:
        cfg["api_key_env"] = str(api_key_env).strip()

    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write(
            "# Dashboard tarafindan yonetilir — vision (image-to-text) ayarlari.\n"
            "# Resolver onceligi: BU DOSYA > CREW_VISION_* env > CREW_USE_LOCAL_VISION (eski) > default openai\n\n"
        )
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=True)
    reset_cache()
    return cfg
