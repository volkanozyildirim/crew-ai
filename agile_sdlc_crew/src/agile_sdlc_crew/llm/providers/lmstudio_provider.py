"""LM Studio provider — OpenAI-compatible local server (/v1).

Ollama model adlari (qwen2.5-coder:7b) LM Studio formatina cevrilir
(qwen2.5-coder-7b ve gerekirse vendor prefix eklenir)."""

import os

from crewai import LLM

from agile_sdlc_crew import credentials

NAME = "lmstudio"

CREDS_SCHEMA = [
    {
        "name": "base_url",
        "label": "Base URL",
        "secret": False,
        "env_fallback": "LMSTUDIO_BASE_URL",
        "placeholder": "http://localhost:1234/v1",
    },
    {
        "name": "api_key",
        "label": "API Key (opsiyonel)",
        "secret": True,
        "env_fallback": "LMSTUDIO_API_KEY",
    },
]


def build(
    model: str,
    max_tokens: int = 4096,
    base_url_env: str | None = None,
    vendor: str = "qwen",
    **kwargs,
) -> LLM:
    base_url = None
    if base_url_env:
        base_url = os.environ.get(base_url_env)
    if not base_url:
        base_url = (
            credentials.get("llm", NAME, "base_url")
            or os.environ.get("LMSTUDIO_BASE_URL")
            or os.environ.get("OLLAMA_BASE_URL")
            or "http://localhost:1234/v1"
        )
    api_key = (
        credentials.get("llm", NAME, "api_key")
        or os.environ.get("LMSTUDIO_API_KEY")
        or "lm-studio"  # litellm bos string'i reddediyor
    )

    lms_model = model.replace(":", "-")
    if "/" not in lms_model:
        lms_model = f"{vendor}/{lms_model}"

    return LLM(
        model=f"openai/{lms_model}",
        base_url=base_url,
        api_key=api_key,
        max_tokens=max_tokens,
    )
