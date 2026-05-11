"""Embedding provider registry — text → vector cevirme.

LLM kayit sistemi ile paralel mimari:
    Provider (registry.py)  : isimle kayit edilen embedding factory'leri
                              (ollama, openai)
    Resolver (resolver.py)  : config-aware mevcut provider/model/base_url
                              cozumlemesi + write helpers

Yeni backend ekleme: src/agile_sdlc_crew/embed/providers/<name>_provider.py
yaz, registry._bootstrap_builtin_providers icine ekle.
"""

from agile_sdlc_crew.embed.registry import (
    embed_text,
    list_providers,
    register,
)
from agile_sdlc_crew.embed.resolver import (
    get_api_key,
    get_base_url,
    get_dim,
    get_model,
    get_provider,
    load_config,
    save_config,
    KNOWN_EMBED_DIMS,
)

__all__ = [
    "embed_text",
    "get_api_key",
    "get_base_url",
    "get_dim",
    "get_model",
    "get_provider",
    "list_providers",
    "load_config",
    "register",
    "save_config",
    "KNOWN_EMBED_DIMS",
]
