"""Embedding provider registry."""

import logging
from typing import Callable

log = logging.getLogger("pipeline")

EmbedFactory = Callable[..., list[float]]

_PROVIDERS: dict[str, EmbedFactory] = {}


def register(name: str, factory: EmbedFactory) -> None:
    _PROVIDERS[name] = factory


def list_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def get_credential_schemas() -> dict[str, list[dict]]:
    """Provider name -> CREDS_SCHEMA list."""
    from agile_sdlc_crew.embed.providers import (
        fastembed_provider,
        ollama_provider,
        openai_provider,
    )
    schemas: dict[str, list[dict]] = {}
    for mod in (fastembed_provider, ollama_provider, openai_provider):
        schemas[mod.NAME] = list(getattr(mod, "CREDS_SCHEMA", []))
    return schemas


def embed_text(provider: str, text: str, model: str, **kwargs) -> list[float]:
    if provider not in _PROVIDERS:
        raise ValueError(
            f"Bilinmeyen embedding provider: {provider!r}. "
            f"Kayitli: {sorted(_PROVIDERS.keys())}"
        )
    return _PROVIDERS[provider](text=text, model=model, **kwargs)


def _bootstrap_builtin_providers() -> None:
    from agile_sdlc_crew.embed.providers import (
        fastembed_provider,
        ollama_provider,
        openai_provider,
    )

    for mod in (fastembed_provider, ollama_provider, openai_provider):
        register(mod.NAME, mod.embed)


_bootstrap_builtin_providers()
