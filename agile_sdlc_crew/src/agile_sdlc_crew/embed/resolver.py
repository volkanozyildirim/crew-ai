"""Embedding config resolver — yaml + env override.

Config dosyasi: config/embed_config.yaml. Dashboard tarafindan yazilir.
Field'lar:
    provider:    "ollama" | "openai"
    model:       "mxbai-embed-large" | "text-embedding-3-small" vb.
    base_url:    opsiyonel; bossa env veya provider default'u
    api_key_env: opsiyonel; openai-uyumlu provider icin env adi
    dimension:   opsiyonel; explicit override
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger("pipeline")

_CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "embed_config.yaml"

# Bilinen embedding modeli boyutlari
KNOWN_EMBED_DIMS = {
    # FastEmbed (yerel ONNX — servis gerektirmez)
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "nomic-ai/nomic-embed-text-v1": 768,
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    # FastEmbed multilingual (Turkce + Ingilizce cross-lingual)
    "intfloat/multilingual-e5-large": 1024,
    "intfloat/multilingual-e5-base": 768,
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    # Ollama
    "mxbai-embed-large": 1024,
    "nomic-embed-text": 768,
    "bge-m3": 1024,
    "bge-large": 1024,
    "all-minilm": 384,
    "snowflake-arctic-embed": 1024,
    "snowflake-arctic-embed2": 1024,
    # OpenAI
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


@lru_cache(maxsize=1)
def load_config() -> dict:
    if not _CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning(f"  embed_config.yaml okuma hatasi: {e}")
        return {}


def reset_cache() -> None:
    load_config.cache_clear()


def get_provider() -> str:
    cfg = load_config()
    return (
        cfg.get("provider")
        or os.environ.get("CREW_EMBED_PROVIDER")
        or "fastembed"
    )


def get_model() -> str:
    cfg = load_config()
    return (
        cfg.get("model")
        or os.environ.get("CREW_EMBED_MODEL")
        or "BAAI/bge-small-en-v1.5"
    )


def get_base_url() -> str:
    cfg = load_config()
    if cfg.get("base_url"):
        return cfg["base_url"]
    if os.environ.get("CREW_EMBED_BASE_URL"):
        return os.environ["CREW_EMBED_BASE_URL"]
    # Provider default
    provider = get_provider()
    if provider == "openai":
        return (
            os.environ.get("LITELLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def get_api_key() -> str:
    cfg = load_config()
    if cfg.get("api_key_env"):
        return os.environ.get(cfg["api_key_env"], "")
    # Default fallback
    return (
        os.environ.get("LITELLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )


def get_dim() -> int:
    cfg = load_config()
    explicit = cfg.get("dimension")
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    model = get_model()
    base = model.split(":")[0]
    return KNOWN_EMBED_DIMS.get(model) or KNOWN_EMBED_DIMS.get(base) or 1024


def save_config(
    provider: str,
    model: str,
    base_url: str = "",
    api_key_env: str = "",
    dimension: int | None = None,
) -> dict:
    """Dashboard'dan gelen ayari kaydet. Sonraki VectorStore() yeni config'i kullanir."""
    from agile_sdlc_crew.embed.registry import list_providers
    if provider not in list_providers():
        raise ValueError(
            f"Bilinmeyen embedding provider: {provider}. Mevcut: {list_providers()}"
        )

    cfg: dict = {"provider": str(provider), "model": str(model)}
    if base_url:
        cfg["base_url"] = str(base_url).strip()
    if api_key_env:
        cfg["api_key_env"] = str(api_key_env).strip()
    if isinstance(dimension, int) and dimension > 0:
        cfg["dimension"] = int(dimension)

    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as fh:
        fh.write(
            "# Dashboard tarafindan yonetilir — embedding ayarlari.\n"
            "# Resolver onceligi: BU DOSYA > CREW_EMBED_* env > provider default\n"
            "# Modeli/dim'i degistirirsen vector DB temizlenmeli.\n\n"
        )
        yaml.safe_dump(cfg, fh, allow_unicode=True, sort_keys=True)
    reset_cache()
    return cfg
