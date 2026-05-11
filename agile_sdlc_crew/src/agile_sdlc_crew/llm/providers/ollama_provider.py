"""Ollama provider — local LLM, http://localhost:11434 default.

Profil yaml'da `base_url_env` belirtilirse o env'den base URL alinir
(orn. OLLAMA_CODER_BASE_URL — coder modeli farkli makinede)."""

import os

from crewai import LLM

from agile_sdlc_crew import credentials

NAME = "ollama"

CREDS_SCHEMA = [
    {
        "name": "base_url",
        "label": "Base URL",
        "secret": False,
        "env_fallback": "OLLAMA_BASE_URL",
        "placeholder": "http://localhost:11434",
    },
]


def build(
    model: str,
    max_tokens: int = 4096,
    base_url_env: str | None = None,
    **kwargs,
) -> LLM:
    base_url = None
    if base_url_env:
        base_url = os.environ.get(base_url_env)
    if not base_url:
        base_url = (
            credentials.get("llm", NAME, "base_url")
            or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        )

    if not model.startswith("ollama/"):
        model = f"ollama/{model}"

    return LLM(
        model=model,
        base_url=base_url,
        max_tokens=max_tokens,
    )
