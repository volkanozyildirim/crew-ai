"""LLM provider registry — provider isimleri ile LLM factory'leri eslestirir."""

import logging
from typing import Callable

from crewai import LLM

log = logging.getLogger("pipeline")

ProviderFactory = Callable[..., LLM]

_PROVIDERS: dict[str, ProviderFactory] = {}


def register(name: str, factory: ProviderFactory) -> None:
    """Bir provider'i registry'e ekler. Ayni isim varsa uzerine yazar."""
    _PROVIDERS[name] = factory


def build_llm(provider: str, model: str, max_tokens: int = 4096, **kwargs) -> LLM:
    """Kayitli bir provider uzerinden LLM olusturur."""
    if provider not in _PROVIDERS:
        raise ValueError(
            f"Bilinmeyen LLM provider: {provider!r}. "
            f"Kayitli: {sorted(_PROVIDERS.keys())}"
        )
    return _PROVIDERS[provider](model=model, max_tokens=max_tokens, **kwargs)


def list_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def get_credential_schemas() -> dict[str, list[dict]]:
    """Provider name -> CREDS_SCHEMA (list of field defs).

    Module-level CREDS_SCHEMA atributlerini toplar."""
    from agile_sdlc_crew.llm.providers import (
        anthropic_provider,
        claude_cli_provider,
        litellm_provider,
        lmstudio_provider,
        ollama_provider,
    )
    schemas: dict[str, list[dict]] = {}
    for mod in (litellm_provider, anthropic_provider, claude_cli_provider,
                ollama_provider, lmstudio_provider):
        schemas[mod.NAME] = list(getattr(mod, "CREDS_SCHEMA", []))
    return schemas


def _bootstrap_builtin_providers() -> None:
    """Built-in providerlari import edip registry'e kaydeder."""
    from agile_sdlc_crew.llm.providers import (
        anthropic_provider,
        claude_cli_provider,
        litellm_provider,
        lmstudio_provider,
        ollama_provider,
    )

    for mod in (litellm_provider, anthropic_provider, claude_cli_provider,
                ollama_provider, lmstudio_provider):
        register(mod.NAME, mod.build)


_bootstrap_builtin_providers()
