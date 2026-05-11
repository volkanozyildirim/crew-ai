"""Vision provider registry."""

import logging
from typing import Callable

log = logging.getLogger("pipeline")

VisionFactory = Callable[..., str]

_PROVIDERS: dict[str, VisionFactory] = {}


def register(name: str, factory: VisionFactory) -> None:
    _PROVIDERS[name] = factory


def list_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def analyze_image(
    provider: str,
    image_b64: str,
    mime: str,
    prompt: str,
    model: str,
    **kwargs,
) -> str:
    """Bir provider uzerinden gorsel analizi calistir.

    Provider implementasyonlari (image_b64, mime, prompt, model, **kw) imzasiyla
    bir string (textual aciklama) doner."""
    if provider not in _PROVIDERS:
        raise ValueError(
            f"Bilinmeyen vision provider: {provider!r}. "
            f"Kayitli: {sorted(_PROVIDERS.keys())}"
        )
    return _PROVIDERS[provider](
        image_b64=image_b64, mime=mime, prompt=prompt, model=model, **kwargs,
    )


def get_credential_schemas() -> dict[str, list[dict]]:
    """Provider name -> CREDS_SCHEMA list."""
    from agile_sdlc_crew.vision.providers import (
        ollama_provider,
        openai_provider,
    )
    schemas: dict[str, list[dict]] = {}
    for mod in (ollama_provider, openai_provider):
        schemas[mod.NAME] = list(getattr(mod, "CREDS_SCHEMA", []))
    return schemas


def _bootstrap_builtin_providers() -> None:
    from agile_sdlc_crew.vision.providers import (
        ollama_provider,
        openai_provider,
    )
    for mod in (ollama_provider, openai_provider):
        register(mod.NAME, mod.analyze)


_bootstrap_builtin_providers()
